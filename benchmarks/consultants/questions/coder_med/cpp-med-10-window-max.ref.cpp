#include <iostream>
#include <vector>
#include <deque>
int main(){
    long k;
    if(!(std::cin >> k)) return 0;
    std::vector<long> a; long x;
    while(std::cin >> x) a.push_back(x);
    std::deque<long> dq;
    bool first=true;
    for(long i=0;i<(long)a.size();i++){
        while(!dq.empty() && a[dq.back()] <= a[i]) dq.pop_back();
        dq.push_back(i);
        if(dq.front() <= i-k) dq.pop_front();
        if(i >= k-1){ if(!first) std::cout<<" "; std::cout<<a[dq.front()]; first=false; }
    }
    std::cout << "\n";
    return 0;
}
