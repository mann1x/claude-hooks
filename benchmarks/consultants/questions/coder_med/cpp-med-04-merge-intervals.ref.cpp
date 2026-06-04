#include <iostream>
#include <vector>
#include <algorithm>
int main(){
    long n;
    if(!(std::cin >> n)) return 0;
    std::vector<std::pair<long,long>> iv(n);
    for(long i=0;i<n;i++) std::cin >> iv[i].first >> iv[i].second;
    std::sort(iv.begin(), iv.end());
    std::vector<std::pair<long,long>> out;
    for(auto &p : iv){
        if(!out.empty() && p.first <= out.back().second)
            out.back().second = std::max(out.back().second, p.second);
        else out.push_back(p);
    }
    for(size_t i=0;i<out.size();i++){
        std::cout << out[i].first << " " << out[i].second;
        if(i+1<out.size()) std::cout << " ";
    }
    std::cout << "\n";
    return 0;
}
