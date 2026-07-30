#include <iostream>
#include <string>
#include <map>
int main(){
    std::string s;
    std::cin >> s;
    std::map<char,int> v{{'I',1},{'V',5},{'X',10},{'L',50},
        {'C',100},{'D',500},{'M',1000}};
    long total = 0;
    int n = (int)s.size();
    for(int i=0;i<n;i++){
        if(i+1<n && v[s[i]]<v[s[i+1]]) total -= v[s[i]];
        else total += v[s[i]];
    }
    std::cout << total << "\n";
    return 0;
}
