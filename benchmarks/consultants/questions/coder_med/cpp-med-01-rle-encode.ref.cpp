#include <iostream>
#include <string>
int main(){
    std::string s;
    std::getline(std::cin, s);
    while(!s.empty() && (s.back()=='\r'||s.back()=='\n')) s.pop_back();
    std::string out;
    size_t i=0, n=s.size();
    while(i<n){
        size_t j=i;
        while(j<n && s[j]==s[i]) j++;
        out += s[i];
        out += std::to_string((int)(j-i));
        i=j;
    }
    std::cout << out << "\n";
    return 0;
}
